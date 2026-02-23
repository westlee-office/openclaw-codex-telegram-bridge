# OpenClaw + Codex Telegram Bridge

This is a minimal Telegram bot bridge that routes replies through:

1. OpenClaw CLI first
2. Codex CLI fallback/refinement when needed

## Behavior

- `auto` (default): OpenClaw first, fallback to Codex if OpenClaw output looks weak.
- `claw`: OpenClaw only.
- `codex`: Codex only.
- `hybrid`: OpenClaw draft, then Codex rewrites/finalizes every time.
- In `codex` mode, each chat keeps a persistent Codex session id and auto-resumes.
- Session state is compacted periodically and mirrored to `memory.md`.
- Replies append a `context left` footer (estimated from latest Codex usage).
- Recommended default model profile: `gpt-5.3-codex` + `model_reasoning_effort="xhigh"`.

## Requirements

- Python 3.9+
- `openclaw` CLI installed and usable in shell
- `codex` CLI installed and authenticated
- Telegram bot token from BotFather

## Setup

```bash
cd /Users/westlee/Projects/openclaw-codex-telegram-bridge
cp .env.example .env
```

Edit `.env` with at least:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_DEFAULT_CHAT_ID` (for cron delivery)
- `TELEGRAM_LOG_ENABLED=true`
- `TELEGRAM_LOG_PATH=logs/telegram_history.jsonl`
- Optional but recommended: `CONTEXT_WINDOW_TOKENS` (default `200000`)
- Optional routing/settings values

## Run

```bash
cd /Users/westlee/Projects/openclaw-codex-telegram-bridge
python3 bridge.py
```

`bridge.py` auto-loads `.env` in the current directory.

## Auto Start (launchd, macOS)

The LaunchAgent plist is included as:

- `launchd.com.westlee.openclaw_codex.telegram.bridge.plist`

Install and start:

```bash
cd /Users/westlee/Projects/openclaw-codex-telegram-bridge
cp launchd.com.westlee.openclaw_codex.telegram.bridge.plist ~/Library/LaunchAgents/com.westlee.openclaw_codex.telegram.bridge.plist
launchctl bootout gui/$(id -u)/com.westlee.openclaw_codex.telegram.bridge 2>/dev/null || true
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.westlee.openclaw_codex.telegram.bridge.plist
launchctl enable gui/$(id -u)/com.westlee.openclaw_codex.telegram.bridge
launchctl kickstart -k gui/$(id -u)/com.westlee.openclaw_codex.telegram.bridge
```

Check status:

```bash
launchctl print gui/$(id -u)/com.westlee.openclaw_codex.telegram.bridge
```

## Telegram Commands

- `/help`
- `/status`
- `/mode auto|claw|codex|hybrid`
- `/session`
- `/resume <session_id>`
- `/newsession`
- `/memory`

## Conversation Logs

Bridge and cron Telegram messages are appended to JSONL:

- `logs/telegram_history.jsonl`
- `logs/codex_usage.jsonl` (token usage ledger)
- `logs/sessions/<chat_id>/state.json` (session + resume state)
- `logs/sessions/<chat_id>/memory.md` (compacted memory)

Quick check:

```bash
cd /Users/westlee/Projects/openclaw-codex-telegram-bridge
tail -n 20 logs/telegram_history.jsonl
tail -n 20 logs/codex_usage.jsonl
```

## Cron Jobs (one-shot)

Use `cron_run.py` to run one turn on schedule and optionally send to Telegram.

Examples:

```bash
cd /Users/westlee/Projects/openclaw-codex-telegram-bridge
python3 cron_run.py --mode codex --prompt "한 줄로 오늘 목표 알려줘"
python3 cron_run.py --mode hybrid --chat-id <CHAT_ID> --prompt-file cron/prompts/lunch_checkin.txt --show-route
python3 cron_run.py --mode auto --chat-id <CHAT_ID> --prompt-file cron/prompts/evening_summary.txt --show-route
```

If `--chat-id` is omitted, `cron_run.py` uses `TELEGRAM_DEFAULT_CHAT_ID` from `.env`.

Find your chat id:

```bash
cd /Users/westlee/Projects/openclaw-codex-telegram-bridge
python3 telegram_chat_ids.py
```

If it says no updates, send any message to your bot first, then run again.

Prepared template:

- `crontab.example`
- `cron/prompts/morning_briefing.txt`
- `cron/prompts/lunch_checkin.txt`
- `cron/prompts/evening_summary.txt`

Install cron entries:

```bash
cd /Users/westlee/Projects/openclaw-codex-telegram-bridge
crontab -e
# paste lines from crontab.example
```

## Notes

- OpenClaw command used by default:
  - `openclaw agent --agent main --message "<text>" --json`
- Codex command used by default:
  - `codex exec --skip-git-repo-check --ephemeral -C $CODEX_WORKDIR -o <tmpfile> "<prompt>"`
- For private use, set `TELEGRAM_ALLOWED_CHAT_IDS` to your user/group IDs.
