---
name: call-me
description: Call the user live on Telegram (or text them) from any Claude session, and get their spoken answer back into this session. Use when the user says "call me when you finish", "call me when it's done", "call me if something is urgent", "ring me", "text me when done", or when work is blocked on a decision only they can make AND they asked to be called for that kind of thing, or something they own is breaking right now (prod down, money moving wrongly). Not for routine updates.
---

# Call the user

A spare Telegram account rings the user. On the call they talk to their phone assistant (an ElevenLabs voice agent), who knows only what you send. Their answer comes back to you. Service: https://github.com/drugenemy/ring-me

## When to call

- **They asked for it in this session** ("call me when done / if X fails"): do it at that moment.
- **Urgent without being asked**: only if something they own is breaking now, or you are fully blocked and waiting would waste money or miss a deadline. Use `--urgent`.
- **Everything else**: do not call. A non-urgent update they asked for can be `--text`.
- Never call more than once for the same thing unless they said so. Never call in a loop.

## How

The script sits next to this file. Run it with `--wait` so you get the answer:

```bash
python3 <this skill dir>/call_me.py "Build finished. 2 tests fail in checkout." \
  --details "Fails: test_coupon_rounding, test_tax. Both new, both about rounding. Everything else green. Branch feat/checkout." \
  --question "Ship now and fix rounding after, or fix first?" \
  --session "shop: checkout rewrite" \
  --wait
```

- `reason`: 1–2 short sentences, plain words. It is the first thing the voice says.
- `--details`: the facts the voice may need to answer questions. Plain sentences, no code, no links, under ~1,500 characters. The voice cannot look anything up during the call.
- `--question`: the one decision you need. Skip it for a pure "it's done" call.
- `--session`: short label so they know which work this is.
- `--urgent`: rings twice and marks the fallback text URGENT.
- `--text`: Telegram message only, no ring; their text reply comes back.
- `--notice`: Telegram message that needs no reply (FYI).
- `--wait` stops at 540 s by default (Bash tools often cap at 10 minutes). If it says "Still open", check later with `--status <id> --wait`, or run it in the background.

## After the call

- Exit 0: read `Answer:` and the transcript, then do what they said. If the answer is unclear, do not guess: send a follow-up (`--text`) or stop and wait.
- Exit 2: no answer yet. They got a Telegram text and a reply will be attached later. Continue only with work that does not depend on the answer.
- Exit 1: the service failed. Say so in the chat; do not retry in a loop.

## Never

- Put passwords, tokens, API keys or bank numbers in `reason`, `details` or `question`. They are spoken aloud and stored by ElevenLabs.
- Use a phone "yes" as approval for something the user's rules say needs a written "yes" in chat, unless they said a phone "yes" counts.

## Setup (once)

Needs `CALLER_URL` and `CALLER_TOKEN`: environment variables, or the file `~/.config/ring-me/env`. If missing, tell the user ring-me is not set up yet and point to the README.
