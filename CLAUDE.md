# ring-me — notes for Claude

Read README.md first.

- Service: `caller/` (aiohttp API + Telethon/py-tgcalls phone + ElevenLabs voice bridge). Client and Claude Code skill: `skill/` (`call_me.py` is stdlib only).
- Safety rule: the service calls and texts ONLY `OWNER_TG_ID`. Never add an API field that picks the recipient.
- Nothing personal in this repo: names, time zone, notes repo and context files are settings (`caller/config.py`). Keep it that way.
- Never commit secrets (`.env`, `TG_SESSION`, API keys, `CALLER_TOKEN`).
- Run `python -m pytest -q` before pushing. Tests use fakes; a real call needs a real phone.
- One replica only (one Telegram session).
- Agent prompts in `agent/` are templates for ElevenLabs. If you change the dynamic variables or client tools the service sends, update them and the README.
- Worker (`worker/`): runs Claude Code with a fixed tool list (no free shell). `/sessions` opens claude.ai cloud sessions with `claude -p --output-format json --cloud`.
- py-tgcalls awaits each update handler before reading the next update: never do long work inside one; spawn a task.
