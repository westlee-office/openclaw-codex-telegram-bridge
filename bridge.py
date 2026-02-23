#!/usr/bin/env python3
"""Telegram bridge that routes messages through OpenClaw and Codex CLI."""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple


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
    usage: Optional[Dict[str, int]] = None
    session_id: str = ""


@dataclass
class Config:
    telegram_token: str
    allowed_chat_ids: Optional[set]
    router_mode: str
    show_route_debug: bool
    telegram_log_enabled: bool
    telegram_log_path: str
    codex_usage_log_path: str
    context_window_tokens: int
    usage_include_cached_tokens: bool
    usage_footer_enabled: bool
    sessions_root_dir: str
    session_recent_turns: int
    session_compact_every_turns: int
    session_bootstrap_max_chars: int
    session_memory_max_chars: int
    session_compact_summary_max_chars: int
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
            codex_usage_log_path=(
                os.getenv("CODEX_USAGE_LOG_PATH", "logs/codex_usage.jsonl").strip()
                or "logs/codex_usage.jsonl"
            ),
            context_window_tokens=max(0, env_int("CONTEXT_WINDOW_TOKENS", 200000)),
            usage_include_cached_tokens=env_bool("USAGE_INCLUDE_CACHED_TOKENS", True),
            usage_footer_enabled=env_bool("USAGE_FOOTER_ENABLED", True),
            sessions_root_dir=(
                os.getenv("SESSIONS_ROOT_DIR", "logs/sessions").strip()
                or "logs/sessions"
            ),
            session_recent_turns=max(1, env_int("SESSION_RECENT_TURNS", 24)),
            session_compact_every_turns=max(
                0, env_int("SESSION_COMPACT_EVERY_TURNS", 40)
            ),
            session_bootstrap_max_chars=max(
                1000, env_int("SESSION_BOOTSTRAP_MAX_CHARS", 12000)
            ),
            session_memory_max_chars=max(300, env_int("SESSION_MEMORY_MAX_CHARS", 4000)),
            session_compact_summary_max_chars=max(
                300, env_int("SESSION_COMPACT_SUMMARY_MAX_CHARS", 8000)
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
    include_usage_footer: bool = False,
    context_usage: Optional[Dict[str, int]] = None,
) -> None:
    body = (
        append_context_footer(text, cfg, usage=context_usage)
        if include_usage_footer
        else text
    )
    send_message(cfg.telegram_token, chat_id, body)
    payload = {"kind": kind, "chat_id": chat_id, "text": body}
    if extra:
        payload.update(extra)
    log_telegram_event(cfg.telegram_log_enabled, cfg.telegram_log_path, "outgoing", payload)


def ensure_parent_dir(path: str) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)


def truncate_text(text: str, max_chars: int) -> str:
    txt = (text or "").strip()
    if max_chars <= 0 or len(txt) <= max_chars:
        return txt
    return txt[: max_chars - 1].rstrip() + "…"


def session_dir_for_chat(cfg: Config, chat_id: int) -> str:
    return os.path.join(cfg.sessions_root_dir, str(chat_id))


def session_state_path(cfg: Config, chat_id: int) -> str:
    return os.path.join(session_dir_for_chat(cfg, chat_id), "state.json")


def session_memory_path(cfg: Config, chat_id: int) -> str:
    return os.path.join(session_dir_for_chat(cfg, chat_id), "memory.md")


def default_session_state(chat_id: int) -> dict:
    return {
        "chat_id": chat_id,
        "active_codex_session_id": "",
        "archived_codex_session_ids": [],
        "turn_count": 0,
        "since_compaction": 0,
        "compact_summary": "",
        "recent_turns": [],
        "last_usage": {},
        "updated_at": utc_now_iso(),
    }


def normalize_session_state(chat_id: int, raw: Any) -> dict:
    state = default_session_state(chat_id)
    if not isinstance(raw, dict):
        return state
    if isinstance(raw.get("active_codex_session_id"), str):
        state["active_codex_session_id"] = raw["active_codex_session_id"].strip()
    archived = raw.get("archived_codex_session_ids")
    if isinstance(archived, list):
        state["archived_codex_session_ids"] = [x for x in archived if isinstance(x, str)]
    if isinstance(raw.get("turn_count"), int):
        state["turn_count"] = max(0, raw["turn_count"])
    if isinstance(raw.get("since_compaction"), int):
        state["since_compaction"] = max(0, raw["since_compaction"])
    if isinstance(raw.get("compact_summary"), str):
        state["compact_summary"] = raw["compact_summary"]
    recent = raw.get("recent_turns")
    if isinstance(recent, list):
        parsed: List[List[str]] = []
        for row in recent:
            if (
                isinstance(row, list)
                and len(row) == 2
                and isinstance(row[0], str)
                and isinstance(row[1], str)
            ):
                parsed.append([row[0], row[1]])
        state["recent_turns"] = parsed
    usage = raw.get("last_usage")
    if isinstance(usage, dict):
        state["last_usage"] = {
            "input_tokens": int(usage.get("input_tokens", 0) or 0),
            "cached_input_tokens": int(usage.get("cached_input_tokens", 0) or 0),
            "output_tokens": int(usage.get("output_tokens", 0) or 0),
        }
    return state


def load_session_state(cfg: Config, chat_id: int) -> dict:
    path = session_state_path(cfg, chat_id)
    if not os.path.exists(path):
        return default_session_state(chat_id)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return default_session_state(chat_id)
    return normalize_session_state(chat_id, data)


def save_session_state(cfg: Config, chat_id: int, state: dict) -> None:
    path = session_state_path(cfg, chat_id)
    os.makedirs(session_dir_for_chat(cfg, chat_id), exist_ok=True)
    clean = normalize_session_state(chat_id, state)
    clean["updated_at"] = utc_now_iso()
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def read_text_file(path: str, default: str = "") -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return default


def write_text_file(path: str, text: str) -> None:
    ensure_parent_dir(path)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def ensure_memory_file(cfg: Config, chat_id: int) -> str:
    path = session_memory_path(cfg, chat_id)
    if os.path.exists(path):
        return path
    template = (
        "# Memory\n\n"
        "Updated: (not yet)\n\n"
        "## Stable Preferences\n"
        "- \n\n"
        "## Ongoing Goals\n"
        "- \n\n"
        "## Important Facts\n"
        "- \n\n"
        "## Compacted Context\n"
        "- \n"
    )
    write_text_file(path, template)
    return path


def append_recent_turn(state: dict, role: str, text: str, max_turns: int) -> None:
    turns = state.get("recent_turns")
    if not isinstance(turns, list):
        turns = []
    turns.append([role, text])
    keep = max(2, max_turns * 2)
    if len(turns) > keep:
        turns = turns[-keep:]
    state["recent_turns"] = turns


def usage_total_tokens(usage: Optional[Dict[str, int]], include_cached: bool) -> int:
    if not usage:
        return 0
    input_tokens = int(usage.get("input_tokens", 0) or 0)
    output_tokens = int(usage.get("output_tokens", 0) or 0)
    cached_tokens = int(usage.get("cached_input_tokens", 0) or 0)
    return input_tokens + output_tokens + (cached_tokens if include_cached else 0)


def record_codex_usage(
    cfg: Config,
    usage: Optional[Dict[str, int]],
    *,
    chat_id: Optional[int],
    session_id: str,
    purpose: str,
) -> None:
    if not usage:
        return
    total = usage_total_tokens(usage, cfg.usage_include_cached_tokens)
    payload = {
        "ts_utc": utc_now_iso(),
        "ts_unix": time.time(),
        "purpose": purpose,
        "chat_id": chat_id,
        "session_id": session_id or "",
        "usage": usage,
        "total_tokens": total,
    }
    try:
        append_jsonl_atomic(cfg.codex_usage_log_path, payload)
    except Exception as exc:
        print(f"[warn] usage logging failed: {exc}", file=sys.stderr)


def latest_usage_from_log(cfg: Config) -> Optional[Dict[str, int]]:
    path = cfg.codex_usage_log_path
    if not os.path.exists(path):
        return None
    last: Optional[Dict[str, int]] = None
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                usage = obj.get("usage")
                if not isinstance(usage, dict):
                    continue
                last = {
                    "input_tokens": int(usage.get("input_tokens", 0) or 0),
                    "cached_input_tokens": int(
                        usage.get("cached_input_tokens", 0) or 0
                    ),
                    "output_tokens": int(usage.get("output_tokens", 0) or 0),
                }
    except Exception:
        return None
    return last


def format_context_footer(
    cfg: Config,
    *,
    usage: Optional[Dict[str, int]] = None,
) -> str:
    if not cfg.usage_footer_enabled:
        return ""
    active_usage = usage if usage else latest_usage_from_log(cfg)
    if not active_usage:
        return "[context left] unknown"
    used = usage_total_tokens(active_usage, cfg.usage_include_cached_tokens)
    limit = cfg.context_window_tokens
    if limit <= 0:
        return f"[context left] unknown (set CONTEXT_WINDOW_TOKENS), used~{used:,}"
    left = max(limit - used, 0)
    pct = (left / limit) * 100 if limit else 0.0
    return f"[context left] {left:,}/{limit:,} ({pct:.1f}%)"


def append_context_footer(
    text: str,
    cfg: Config,
    *,
    usage: Optional[Dict[str, int]] = None,
) -> str:
    footer = format_context_footer(cfg, usage=usage)
    if not footer:
        return text
    clean = (text or "").strip()
    return f"{clean}\n\n{footer}" if clean else footer


def parse_codex_json_events(stdout: str) -> Tuple[str, str, Optional[Dict[str, int]]]:
    session_id = ""
    answer = ""
    usage: Optional[Dict[str, int]] = None
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = obj.get("type")
        if kind == "thread.started" and isinstance(obj.get("thread_id"), str):
            session_id = obj["thread_id"]
        elif kind == "item.completed":
            item = obj.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    answer = text.strip()
        elif kind == "turn.completed":
            raw_usage = obj.get("usage")
            if isinstance(raw_usage, dict):
                usage = {
                    "input_tokens": int(raw_usage.get("input_tokens", 0) or 0),
                    "cached_input_tokens": int(
                        raw_usage.get("cached_input_tokens", 0) or 0
                    ),
                    "output_tokens": int(raw_usage.get("output_tokens", 0) or 0),
                }
    return answer, session_id, usage


def memory_from_recent_turns(state: dict) -> str:
    turns = state.get("recent_turns") if isinstance(state, dict) else []
    if not isinstance(turns, list) or not turns:
        return "- (none yet)"
    lines: List[str] = []
    for role, text in turns[-12:]:
        if not isinstance(role, str) or not isinstance(text, str):
            continue
        tag = "U" if role == "user" else "A"
        compact = re.sub(r"\s+", " ", text.strip())
        if compact:
            lines.append(f"- {tag}: {truncate_text(compact, 180)}")
    return "\n".join(lines) if lines else "- (none yet)"


def write_memory_snapshot(cfg: Config, chat_id: int, state: dict) -> None:
    path = ensure_memory_file(cfg, chat_id)
    summary = truncate_text(
        str(state.get("compact_summary") or ""), cfg.session_compact_summary_max_chars
    )
    recent = memory_from_recent_turns(state)
    text = (
        "# Memory\n\n"
        f"Updated: {utc_now_iso()}\n\n"
        "## Stable Preferences\n"
        "- Respond in the user's language.\n"
        "- Prefer direct, actionable answers.\n\n"
        "## Ongoing Goals\n"
        "- Keep long-running project continuity across Telegram turns.\n\n"
        "## Important Facts\n"
        f"{recent}\n\n"
        "## Compacted Context\n"
        f"{summary if summary else '- (none yet)'}\n"
    )
    write_text_file(path, text)


def build_session_bootstrap_prompt(
    cfg: Config,
    chat_id: int,
    state: dict,
    user_text: str,
) -> str:
    memory_text = truncate_text(
        read_text_file(ensure_memory_file(cfg, chat_id), ""), cfg.session_memory_max_chars
    )
    compact_summary = truncate_text(
        str(state.get("compact_summary") or ""), cfg.session_compact_summary_max_chars
    )
    recent_lines = memory_from_recent_turns(state)
    lines = [
        "This is a resumed Telegram assistant session.",
        "Use the context below to continue naturally.",
        "Do not mention this bootstrap context unless asked.",
        "",
        "Memory.md excerpt:",
        memory_text or "(empty)",
        "",
        "Compacted summary:",
        compact_summary or "(empty)",
        "",
        "Recent turns:",
        recent_lines,
        "",
        "Latest user message:",
        user_text,
        "",
        "Final answer:",
    ]
    prompt = "\n".join(lines)
    return truncate_text(prompt, cfg.session_bootstrap_max_chars)


def compact_state_if_needed(cfg: Config, chat_id: int, state: dict) -> Tuple[dict, bool]:
    threshold = cfg.session_compact_every_turns
    if threshold <= 0:
        return state, False
    since_compaction = int(state.get("since_compaction", 0) or 0)
    if since_compaction < threshold:
        return state, False

    previous = truncate_text(str(state.get("compact_summary") or ""), 3000)
    recent = memory_from_recent_turns(state)
    candidate = (
        f"{previous}\n"
        f"\n[compacted @ {utc_now_iso()}]\n"
        f"{recent}\n"
    ).strip()
    state["compact_summary"] = truncate_text(candidate, cfg.session_compact_summary_max_chars)

    active = str(state.get("active_codex_session_id") or "").strip()
    if active:
        archived = state.get("archived_codex_session_ids")
        if not isinstance(archived, list):
            archived = []
        archived.append(active)
        state["archived_codex_session_ids"] = archived[-20:]
    state["active_codex_session_id"] = ""
    state["since_compaction"] = 0
    turns = state.get("recent_turns")
    if isinstance(turns, list) and len(turns) > 8:
        state["recent_turns"] = turns[-8:]
    write_memory_snapshot(cfg, chat_id, state)
    return state, True


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


def run_codex(
    user_text: str,
    history: Sequence[Tuple[str, str]],
    claw_draft: Optional[str],
    cfg: Config,
    *,
    resume_session_id: Optional[str] = None,
    prompt_override: Optional[str] = None,
) -> ModelResult:
    prompt = (
        prompt_override
        if isinstance(prompt_override, str) and prompt_override.strip()
        else build_codex_prompt(user_text=user_text, history=history, claw_draft=claw_draft)
    )

    resume_session_id = (resume_session_id or "").strip() or None
    if resume_session_id:
        cmd = [cfg.codex_bin, "exec", "resume", "--skip-git-repo-check", "--json"]
        if cfg.codex_model:
            cmd.extend(["-m", cfg.codex_model])
        if cfg.codex_profile:
            cmd.extend(["-p", cfg.codex_profile])
        cmd.extend(cfg.codex_extra_args)
        cmd.extend([resume_session_id, prompt])
    else:
        cmd = [
            cfg.codex_bin,
            "exec",
            "--skip-git-repo-check",
            "--json",
            "-C",
            cfg.codex_workdir,
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
            session_id=resume_session_id or "",
        )
    except FileNotFoundError:
        return ModelResult(
            ok=False,
            text="",
            error=f"codex binary not found: {cfg.codex_bin}",
            session_id=resume_session_id or "",
        )

    answer, parsed_session_id, usage = parse_codex_json_events(stdout)
    session_id = parsed_session_id or (resume_session_id or "")

    if code != 0:
        detail = (stderr or stdout or "").strip() or f"codex exited with code {code}"
        return ModelResult(
            ok=False,
            text=answer,
            error=detail,
            exit_code=code,
            stdout=stdout,
            stderr=stderr,
            usage=usage,
            session_id=session_id,
        )
    if not answer:
        return ModelResult(
            ok=False,
            text="",
            error="codex returned empty output",
            exit_code=code,
            stdout=stdout,
            stderr=stderr,
            usage=usage,
            session_id=session_id,
        )
    return ModelResult(
        ok=True,
        text=answer,
        exit_code=code,
        stdout=stdout,
        stderr=stderr,
        usage=usage,
        session_id=session_id,
    )


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
        record_codex_usage(
            cfg, codex.usage, chat_id=None, session_id=codex.session_id, purpose="router_turn"
        )
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
        record_codex_usage(
            cfg, codex.usage, chat_id=None, session_id=codex.session_id, purpose="router_turn"
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
        record_codex_usage(
            cfg, codex.usage, chat_id=None, session_id=codex.session_id, purpose="router_turn"
        )
        if codex.ok:
            return codex.text, f"codex_fallback({reason})"
        return claw.text, f"claw_used_codex_failed({reason})"

    codex = run_codex(user_text=user_text, history=history, claw_draft=None, cfg=cfg)
    record_codex_usage(
        cfg, codex.usage, chat_id=None, session_id=codex.session_id, purpose="router_turn"
    )
    if codex.ok:
        return codex.text, "codex_fallback(openclaw_error)"
    return f"[openclaw error]\n{claw.error}\n\n[codex error]\n{codex.error}", "both_error"


def codex_turn_with_session(
    cfg: Config,
    chat_id: int,
    user_text: str,
    state: dict,
) -> Tuple[str, str, dict]:
    active_session_id = str(state.get("active_codex_session_id") or "").strip()

    if active_session_id:
        codex = run_codex(
            user_text=user_text,
            history=[],
            claw_draft=None,
            cfg=cfg,
            resume_session_id=active_session_id,
        )
        route = "codex_resume"
    else:
        bootstrap_prompt = build_session_bootstrap_prompt(
            cfg=cfg, chat_id=chat_id, state=state, user_text=user_text
        )
        codex = run_codex(
            user_text=user_text,
            history=[],
            claw_draft=None,
            cfg=cfg,
            prompt_override=bootstrap_prompt,
        )
        route = "codex_new_session"

    if not codex.ok and active_session_id:
        err = codex.error.lower()
        if "session" in err and ("not found" in err or "invalid" in err):
            state["active_codex_session_id"] = ""
            bootstrap_prompt = build_session_bootstrap_prompt(
                cfg=cfg, chat_id=chat_id, state=state, user_text=user_text
            )
            codex = run_codex(
                user_text=user_text,
                history=[],
                claw_draft=None,
                cfg=cfg,
                prompt_override=bootstrap_prompt,
            )
            route = "codex_resume_recovered"

    record_codex_usage(
        cfg,
        codex.usage,
        chat_id=chat_id,
        session_id=codex.session_id or active_session_id,
        purpose="assistant_turn",
    )

    if not codex.ok:
        append_recent_turn(state, "user", user_text, cfg.session_recent_turns)
        save_session_state(cfg, chat_id, state)
        return f"[codex error]\n{codex.error}", "codex_error", state

    if codex.usage:
        state["last_usage"] = {
            "input_tokens": int(codex.usage.get("input_tokens", 0) or 0),
            "cached_input_tokens": int(
                codex.usage.get("cached_input_tokens", 0) or 0
            ),
            "output_tokens": int(codex.usage.get("output_tokens", 0) or 0),
        }

    session_id = codex.session_id or active_session_id
    if session_id:
        state["active_codex_session_id"] = session_id

    append_recent_turn(state, "user", user_text, cfg.session_recent_turns)
    append_recent_turn(state, "assistant", codex.text, cfg.session_recent_turns)
    state["turn_count"] = int(state.get("turn_count", 0) or 0) + 1
    state["since_compaction"] = int(state.get("since_compaction", 0) or 0) + 1
    write_memory_snapshot(cfg, chat_id, state)

    state, compacted = compact_state_if_needed(cfg, chat_id, state)
    if compacted:
        route = f"{route}+compacted"

    save_session_state(cfg, chat_id, state)
    return codex.text, route, state


def session_status_text(chat_id: int, state: dict) -> str:
    active = str(state.get("active_codex_session_id") or "").strip() or "(none)"
    archived = state.get("archived_codex_session_ids")
    archived_count = len(archived) if isinstance(archived, list) else 0
    return (
        f"chat_id={chat_id}\n"
        f"active_session={active}\n"
        f"turn_count={int(state.get('turn_count', 0) or 0)}\n"
        f"since_compaction={int(state.get('since_compaction', 0) or 0)}\n"
        f"archived_sessions={archived_count}"
    )


def command_help(default_mode: str) -> str:
    return (
        "OpenClaw + Codex Telegram bridge is running.\n"
        "Commands:\n"
        "/mode auto|claw|codex|hybrid\n"
        "/session\n"
        "/resume <session_id>\n"
        "/newsession\n"
        "/memory\n"
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
    session_cache: Dict[int, dict] = {}

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

            state = session_cache.get(chat_id)
            if not isinstance(state, dict):
                state = load_session_state(cfg, chat_id)
                session_cache[chat_id] = state
            ensure_memory_file(cfg, chat_id)

            if text.startswith("/start") or text.startswith("/help"):
                help_text = command_help(cfg.router_mode)
                send_and_log(
                    cfg,
                    chat_id,
                    help_text,
                    kind="command_reply",
                    include_usage_footer=True,
                    context_usage=state.get("last_usage"),
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
                        include_usage_footer=True,
                        context_usage=state.get("last_usage"),
                        extra={"command": "mode", "update_id": update_id, "reply_to_message_id": msg_id},
                    )
                else:
                    send_and_log(
                        cfg,
                        chat_id,
                        "Usage: /mode auto|claw|codex|hybrid",
                        kind="command_reply",
                        include_usage_footer=True,
                        context_usage=state.get("last_usage"),
                        extra={"command": "mode", "update_id": update_id, "reply_to_message_id": msg_id},
                    )
                continue

            if text.startswith("/session"):
                send_and_log(
                    cfg,
                    chat_id,
                    session_status_text(chat_id, state),
                    kind="command_reply",
                    include_usage_footer=True,
                    context_usage=state.get("last_usage"),
                    extra={"command": "session", "update_id": update_id, "reply_to_message_id": msg_id},
                )
                continue

            if text.startswith("/resume"):
                parts = text.split(maxsplit=1)
                if len(parts) != 2 or not parts[1].strip():
                    send_and_log(
                        cfg,
                        chat_id,
                        "Usage: /resume <session_id>",
                        kind="command_reply",
                        include_usage_footer=True,
                        context_usage=state.get("last_usage"),
                        extra={"command": "resume", "update_id": update_id, "reply_to_message_id": msg_id},
                    )
                else:
                    state["active_codex_session_id"] = parts[1].strip()
                    save_session_state(cfg, chat_id, state)
                    send_and_log(
                        cfg,
                        chat_id,
                        f"Resumed session: {state['active_codex_session_id']}",
                        kind="command_reply",
                        include_usage_footer=True,
                        context_usage=state.get("last_usage"),
                        extra={"command": "resume", "update_id": update_id, "reply_to_message_id": msg_id},
                    )
                continue

            if text.startswith("/newsession"):
                active = str(state.get("active_codex_session_id") or "").strip()
                if active:
                    archived = state.get("archived_codex_session_ids")
                    if not isinstance(archived, list):
                        archived = []
                    archived.append(active)
                    state["archived_codex_session_ids"] = archived[-20:]
                state["active_codex_session_id"] = ""
                state["since_compaction"] = 0
                state["last_usage"] = {}
                save_session_state(cfg, chat_id, state)
                send_and_log(
                    cfg,
                    chat_id,
                    "Started a fresh session. Previous session archived.",
                    kind="command_reply",
                    include_usage_footer=True,
                    context_usage=state.get("last_usage"),
                    extra={"command": "newsession", "update_id": update_id, "reply_to_message_id": msg_id},
                )
                continue

            if text.startswith("/memory"):
                mem = read_text_file(session_memory_path(cfg, chat_id), "").strip()
                body = mem if mem else "(memory is empty)"
                send_and_log(
                    cfg,
                    chat_id,
                    truncate_text(body, 3000),
                    kind="command_reply",
                    include_usage_footer=True,
                    context_usage=state.get("last_usage"),
                    extra={"command": "memory", "update_id": update_id, "reply_to_message_id": msg_id},
                )
                continue

            if text.startswith("/status"):
                active_mode = mode_by_chat.get(chat_id, cfg.router_mode)
                status_text = (
                    f"mode={active_mode}\n"
                    f"history_turns={cfg.history_turns}\n"
                    f"openclaw_agent={cfg.openclaw_agent}\n"
                    f"codex_workdir={cfg.codex_workdir}\n"
                    f"{session_status_text(chat_id, state)}"
                )
                send_and_log(
                    cfg,
                    chat_id,
                    status_text,
                    kind="command_reply",
                    include_usage_footer=True,
                    context_usage=state.get("last_usage"),
                    extra={"command": "status", "update_id": update_id, "reply_to_message_id": msg_id},
                )
                continue

            send_typing(cfg.telegram_token, chat_id)
            active_mode = mode_by_chat.get(chat_id, cfg.router_mode)
            prior = history.get(chat_id, [])
            context = prior[-(cfg.history_turns * 2) :] if cfg.history_turns > 0 else []

            if active_mode == "codex":
                clean_answer, route_tag, state = codex_turn_with_session(
                    cfg=cfg, chat_id=chat_id, user_text=text, state=state
                )
                session_cache[chat_id] = state
            else:
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
                include_usage_footer=True,
                context_usage=state.get("last_usage"),
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
