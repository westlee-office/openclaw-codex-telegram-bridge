# OpenClaw + Codex Telegram Bridge

This is a minimal Telegram bot bridge that routes replies through:

1. OpenClaw CLI first
2. Codex CLI fallback/refinement when needed

## Behavior

- `auto` (default): OpenClaw first, fallback to Codex if OpenClaw output looks weak.
- `claw`: OpenClaw only.
- `codex`: Codex only.
- `hybrid`: OpenClaw draft, then Codex rewrites/finalizes every time.

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
- Optional routing/settings values

## Run

```bash
cd /Users/westlee/Projects/openclaw-codex-telegram-bridge
python3 bridge.py
```

`bridge.py` auto-loads `.env` in the current directory.

## Telegram Commands

- `/help`
- `/status`
- `/mode auto|claw|codex|hybrid`

## Cron Jobs (one-shot)

Use `cron_run.py` to run one turn on schedule and optionally send to Telegram.

Examples:

```bash
cd /Users/westlee/Projects/openclaw-codex-telegram-bridge
python3 cron_run.py --mode codex --prompt "한 줄로 오늘 목표 알려줘"
python3 cron_run.py --mode hybrid --chat-id <CHAT_ID> --prompt-file cron/prompts/lunch_checkin.txt --show-route
python3 cron_run.py --mode auto --chat-id <CHAT_ID> --prompt-file cron/prompts/evening_summary.txt --show-route
```

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
